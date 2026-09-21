#!/usr/bin/env python3
"""
Scarica Video — app locale per scaricare video con yt-dlp.
YouTube, TikTok, Instagram e centinaia di altri siti. Solo Python stdlib.

- Coda con download in parallelo, progress bar, velocità ed ETA
- Intervallo di minutaggio (DA -> A) con taglio preciso ai keyframe
- Cronologia persistente (SQLite) con apri file / mostra nel Finder / ri-scarica
- Login via cookie del browser per i siti che lo richiedono (TikTok, Instagram)
- Fallback su gallery-dl per foto e caroselli
- Trascrizione locale con speaker (whisper.cpp + diarizzazione sherpa-onnx),
  anche di video/audio già sul Mac (scelti col file picker nativo)
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8642
DEST = os.path.expanduser("~/Movies/Scarica Video")
SUPPORT = os.path.expanduser("~/Library/Application Support/Scarica Video")
DB_PATH = os.path.join(SUPPORT, "history.db")


def find_bin(name):
    # Prima il venv dell'app: yt-dlp lì dentro ha curl_cffi (impersonificazione
    # browser, serve a TikTok & co.) ed è aggiornabile senza toccare Homebrew
    cand = os.path.join(SUPPORT, "venv", "bin", name)
    if os.path.exists(cand):
        return cand
    path = shutil.which(name)
    if path:
        return path
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):  # Apple Silicon / Intel
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return name


YTDLP = find_bin("yt-dlp")
GDL = find_bin("gallery-dl")
FFMPEG = find_bin("ffmpeg")
WHISPER = find_bin("whisper-cli")
# Token per-avvio: le API rispondono solo alla nostra pagina, non ad altri
# siti aperti nel browser (CSRF su 127.0.0.1)
TOKEN = uuid.uuid4().hex

CONFIG_PATH = os.path.join(SUPPORT, "config.json")
CONFIG_DEFAULTS = {"dest": DEST, "quality": "1080", "browser": "none",
                   "concurrent": 3, "fragments": 4,
                   "tlang": "auto", "tspeakers": "auto"}
config = dict(CONFIG_DEFAULTS)
_applied_concurrent = config["concurrent"]

jobs = {}
jobs_order = []
done_count = 0
lock = threading.Lock()
slots = threading.Semaphore(config["concurrent"])
db_lock = threading.Lock()

PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
SPEED_RE = re.compile(r"at\s+([\d.]+\s*[KMGT]?i?B/s|Unknown[^\s]*)")
ETA_RE = re.compile(r"ETA\s+([\d:]+)")
SIZE_RE = re.compile(r"of\s+~?\s*([\d.]+\s*[KMGT]?i?B)")
DEST_RE = re.compile(
    r'(?:Destination:|Merging formats into)\s*"?'
    r'((?:/[^"\n]+?)\.(?:mp4|m4a|webm|mkv|mp3|mov))"?'
)
ALREADY_RE = re.compile(
    r"(/[^\n]+?\.(?:mp4|m4a|webm|mkv|mp3|mov)) has already been downloaded"
)


# ---------------------------------------------------------------- config
def load_config():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        config.update({k: data[k] for k in CONFIG_DEFAULTS if k in data})
    except Exception:
        pass
    apply_config()


def save_config():
    os.makedirs(SUPPORT, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def apply_config():
    global DEST, slots, _applied_concurrent
    DEST = os.path.expanduser(str(config["dest"]))
    os.makedirs(DEST, exist_ok=True)
    # Nuovo semaforo solo se il limite cambia: i job già in coda restano
    # legati al vecchio, quelli nuovi usano il nuovo
    if config["concurrent"] != _applied_concurrent:
        slots = threading.Semaphore(int(config["concurrent"]))
        _applied_concurrent = config["concurrent"]


# ---------------------------------------------------------------- preview
preview_cache = {}
PREVIEW_TTL = 3 * 3600  # gli URL googlevideo scadono dopo ~6h


def resolve_preview(url):
    """URL riproducibile dal <video> della preview. YouTube non ha più formati
    progressivi (video+audio in un file unico via http): si usa il manifest
    HLS master, che WKWebView riproduce nativamente con audio e seek.
    Fallback per altri siti: il miglior formato progressivo <=480p."""
    cached = preview_cache.get(url)
    if cached and time.time() - cached[0] < PREVIEW_TTL:
        return cached[1]
    args = [YTDLP, "--no-playlist", "-j", url]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=40)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    try:
        info = json.loads(out.stdout)
    except ValueError:
        return None
    fmts = info.get("formats") or []
    src = next((f.get("manifest_url") for f in fmts if f.get("manifest_url")),
               None)
    if not src:
        prog = [f for f in fmts
                if f.get("url") and str(f.get("protocol", "")).startswith("http")
                and f.get("vcodec") not in (None, "none")
                and f.get("acodec") not in (None, "none")]
        prog.sort(key=lambda f: f.get("height") or 0)
        low = [f for f in prog if (f.get("height") or 0) <= 480]
        pick = (low or prog)[-1] if (low or prog) else None
        src = pick["url"] if pick else None
    if src:
        preview_cache[url] = (time.time(), src)
    return src


# ---------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(SUPPORT, exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id        TEXT PRIMARY KEY,
                url       TEXT,
                title     TEXT,
                filepath  TEXT,
                quality   TEXT,
                section   TEXT,
                status    TEXT,
                error     TEXT,
                created   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(history)")]
        if "note" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN note TEXT")


def save_history(job):
    with db_lock, db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO history "
            "(id,url,title,filepath,quality,section,status,error,note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (job["id"], job["url"], job.get("file"), job.get("filepath"),
             job.get("quality"), job.get("section") or "", job["status"],
             job.get("error"), job.get("note")),
        )


def finish_job(job):
    """Salva in cronologia e toglie il job dalla memoria (il polling non deve
    ritrasmettere per sempre i job conclusi)."""
    global done_count
    save_history(job)
    with lock:
        done_count += 1
        jobs.pop(job["id"], None)
        try:
            jobs_order.remove(job["id"])
        except ValueError:
            pass


def load_history(limit=200):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM history ORDER BY created DESC, rowid DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- trascrizione
# Tutto in locale: whisper.cpp (Metal) trascrive con timestamp per parola,
# sherpa-onnx riconosce chi parla quando (nessun account/token). I modelli si
# scaricano una volta sola in Application Support. Output accanto al video:
#   <nome>.transcript.txt   — leggibile, con speaker e timecode (per trovare i clip)
#   <nome>.transcript.json  — parole con start/end (riutilizzabile in altri tool)
MODELS_DIR = os.path.join(SUPPORT, "models")
WHISPER_MODEL = "large-v3-turbo"
WHISPER_URL = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
               f"ggml-{WHISPER_MODEL}.bin")
# Release ufficiali k2-fsa/sherpa-onnx ("recongition" è il refuso del tag upstream)
SEG_TAR_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
               "speaker-segmentation-models/"
               "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
           "speaker-recongition-models/"
           "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")
SEG_MODEL = os.path.join(MODELS_DIR, "sherpa-onnx-pyannote-segmentation-3-0",
                         "model.onnx")
EMB_MODEL = os.path.join(MODELS_DIR, "embedding.onnx")
TRANSCRIBE_EXTS = (".mp4", ".m4a", ".webm", ".mkv", ".mp3", ".mov")

tjobs = {}
tjobs_order = []
t_slots = threading.Semaphore(1)   # la trascrizione satura la GPU: una alla volta
WHISPER_PROG_RE = re.compile(r"progress\s*=\s*(\d+)%")


def tset(job, **kw):
    if "status" in kw:
        kw["since"] = time.time()   # per mostrare in UI da quanto dura la fase
    with lock:
        job.update(kw)


def download_file(url, dest, job):
    """Scarica con progress sul job. Scrive su .part e rinomina alla fine."""
    tmp = dest + ".part"

    def hook(blocks, bs, total):
        if total > 0:
            tset(job, progress=min(100.0, blocks * bs * 100.0 / total))

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    urllib.request.urlretrieve(url, tmp, hook)
    os.replace(tmp, dest)


def ensure_whisper_model(job):
    path = os.path.join(MODELS_DIR, f"ggml-{WHISPER_MODEL}.bin")
    if not os.path.exists(path):
        tset(job, status="scarico il modello di trascrizione (~1.6 GB, una volta sola)",
             progress=0.0)
        download_file(WHISPER_URL, path, job)
    return path


def ensure_diar_models(job):
    if not os.path.exists(SEG_MODEL):
        tset(job, status="scarico i modelli speaker (una volta sola)", progress=0.0)
        with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=False) as f:
            tmp = f.name
        try:
            download_file(SEG_TAR_URL, tmp, job)
            with tarfile.open(tmp, "r:bz2") as tf:
                member = next(m for m in tf.getmembers()
                              if m.name.endswith("/model.onnx"))
                os.makedirs(os.path.dirname(SEG_MODEL), exist_ok=True)
                with tf.extractfile(member) as src, open(SEG_MODEL, "wb") as out:
                    shutil.copyfileobj(src, out)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if not os.path.exists(EMB_MODEL):
        tset(job, status="scarico i modelli speaker (una volta sola)", progress=0.0)
        download_file(EMB_URL, EMB_MODEL, job)


def ensure_python_deps(job):
    """sherpa-onnx + numpy nel venv dell'app: installati al primo uso, così le
    app già costruite funzionano senza rifare la build."""
    try:
        import sherpa_onnx  # noqa: F401
        import numpy        # noqa: F401
        return
    except ImportError:
        pass
    tset(job, status="installo i componenti speaker (una volta sola)", progress=0.0)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check", "sherpa-onnx", "numpy"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("installazione sherpa-onnx fallita: "
                           + (r.stderr or r.stdout or "").strip()[-300:])
    import importlib
    importlib.invalidate_caches()


def extract_audio(video, wav):
    r = subprocess.run([FFMPEG, "-y", "-i", video, "-vn", "-ar", "16000",
                        "-ac", "1", "-c:a", "pcm_s16le", wav],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("estrazione audio fallita: "
                           + (r.stderr or "").strip().splitlines()[-1][:300])


def run_whisper(wav, model, lang, out_base, job):
    args = [WHISPER, "-m", model, "-f", wav, "-l", lang,
            "-ojf", "-of", out_base, "-pp"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    tail = []
    for line in proc.stderr:
        line = line.rstrip()
        if line:
            tail.append(line)
            tail = tail[-4:]
        m = WHISPER_PROG_RE.search(line)
        if m:
            tset(job, progress=float(m.group(1)))
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("whisper-cli fallito: " + (tail[-1] if tail else
                                                      f"exit {proc.returncode}"))


def tokens_to_words(full):
    """Token whisper.cpp (-ojf) -> parole con start/end in secondi."""
    words = []
    for seg in full.get("transcription") or []:
        for tok in seg.get("tokens") or []:
            # token speciali: [_BEG_], [_EOT_], [_TT_488] (timestamp), ecc.
            raw = re.sub(r"\[_[^\]]*\]", "", tok.get("text") or "")
            if raw.strip() == "":
                continue
            offs = tok.get("offsets") or {}
            start = (offs.get("from") or 0) / 1000
            end = (offs.get("to") or 0) / 1000
            if raw.startswith(" ") or not words:
                words.append({"text": raw.strip(), "start": start, "end": end})
            else:
                words[-1]["text"] += raw
                words[-1]["end"] = end
    return [w for w in words if w["text"]]


# Worker eseguito in un python separato: la chiamata nativa sd.process()
# tiene il GIL per TUTTA la durata (minuti sui video lunghi) — dentro il
# processo dell'app congelava UI e API senza alcun messaggio di errore.
DIARIZE_WORKER = r'''
import json, sys, wave
import numpy as np
import sherpa_onnx
wav, n, seg_model, emb_model, out_path = (sys.argv[1], int(sys.argv[2]),
                                          sys.argv[3], sys.argv[4], sys.argv[5])
# NB: niente num_threads — il thread singolo e' la configurazione validata
# (52 min di audio in ~6.5 min a macchina scarica). I tentativi multi-thread
# non hanno mai avuto un benchmark pulito: con editor video o VM aperti
# macOS relega questo worker sugli efficiency core e qualunque misura salta.
config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
            model=seg_model),
    ),
    embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb_model),
    # threshold alto = cluster piu' stabili: 0.5 spezzava la stessa voce
    # in piu' speaker (testato su un servizio TV multi-voce)
    clustering=sherpa_onnx.FastClusteringConfig(num_clusters=n, threshold=0.8),
    min_duration_on=0.3,
    min_duration_off=0.5,
)
sd = sherpa_onnx.OfflineSpeakerDiarization(config)
with wave.open(wav, "rb") as w:
    if w.getframerate() != sd.sample_rate:
        raise SystemExit(f"sample rate {w.getframerate()} != {sd.sample_rate}")
    frames = w.readframes(w.getnframes())
samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

# Avanzamento su stderr (una riga per punto percentuale): l'app lo legge e
# muove la barra — mai piu' fasi lunghe che sembrano bloccate
last = [-1]
def cb(processed, total):
    pct = int(processed * 100 / max(1, total))
    if pct != last[0]:
        last[0] = pct
        print(f"PROG {pct}", file=sys.stderr, flush=True)
    return 0

result = sd.process(samples, callback=cb).sort_by_start_time()
with open(out_path, "w") as f:
    json.dump([{"start": s.start, "end": s.end, "speaker": s.speaker}
               for s in result], f)
'''


def diarize_wav(wav, num_speakers, on_progress=None):
    """[{start,end,speaker}] con sherpa-onnx in un sottoprocesso.
    num_speakers=-1 -> auto. on_progress(pct) via le righe PROG su stderr;
    il risultato passa da file temporaneo (il JSON puo' superare il buffer
    della pipe). Watchdog a 3 ore contro i processi zombie."""
    out = os.path.join(tempfile.gettempdir(), f"diar-{uuid.uuid4().hex[:8]}.json")
    p = subprocess.Popen([sys.executable, "-c", DIARIZE_WORKER, wav,
                          str(num_speakers), SEG_MODEL, EMB_MODEL, out],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True)
    watchdog = threading.Timer(3 * 3600, p.kill)
    watchdog.daemon = True
    watchdog.start()
    tail = []
    try:
        for line in p.stderr:
            line = line.strip()
            if line.startswith("PROG "):
                if on_progress:
                    try:
                        # la segmentazione copre ~il grosso del tempo: cap a 95,
                        # il resto (embedding+cluster) chiude al "fatto"
                        on_progress(min(95.0, float(line[5:])))
                    except ValueError:
                        pass
            elif line:
                tail.append(line)
                tail = tail[-4:]
        p.wait()
    finally:
        watchdog.cancel()
    if p.returncode != 0:
        raise RuntimeError("diarizzazione fallita: " + " ".join(tail)[-300:])
    try:
        with open(out) as f:
            return json.load(f)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def assign_speakers(words, segments):
    """A ogni parola lo speaker del segmento che ne contiene il punto medio
    (o, se nessuno, quello più vicino)."""
    if not segments:
        return
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        hit = next((s for s in segments if s["start"] <= mid < s["end"]), None)
        if hit is None:
            hit = min(segments, key=lambda s: s["start"] - mid if mid < s["start"]
                      else mid - s["end"])
        w["speaker"] = hit["speaker"]


def fmt_ts(t):
    t = max(0, int(t))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def render_transcript_txt(words, source, lang):
    """Testo leggibile: un blocco per turno di parola, timecode a inizio turno
    e marker intermedi ogni ~30 s (servono a trovare i punti da clippare)."""
    order = []                      # speaker in ordine di prima apparizione
    for w in words:
        sp = w.get("speaker")
        if sp is not None and sp not in order:
            order.append(sp)
    label = {sp: f"SPEAKER {i + 1}" for i, sp in enumerate(order)}

    turns = []
    for w in words:
        sp = w.get("speaker")
        if not turns or turns[-1]["speaker"] != sp:
            turns.append({"speaker": sp, "start": w["start"], "words": []})
        turns[-1]["words"].append(w)

    lines = [f"# Transcript: {source}",
             f"# Lingua: {lang} — speaker: {len(order) or 'non riconosciuti'}",
             ""]
    for t in turns:
        head = f"[{fmt_ts(t['start'])}]"
        if t["speaker"] is not None:
            head += f" {label[t['speaker']]}:"
        lines.append(head)
        buf, last_mark = [], t["start"]
        for w in t["words"]:
            if (w["start"] - last_mark >= 30 and buf
                    and buf[-1].endswith((".", "?", "!"))):
                buf.append(f"[{fmt_ts(w['start'])}]")
                last_mark = w["start"]
            buf.append(w["text"])
        lines.append(" ".join(buf))
        lines.append("")
    return "\n".join(lines), len(order)


def run_transcription(job_id):
    with t_slots:
        job = tjobs[job_id]
        video = job["path"]
        wav = os.path.join(tempfile.gettempdir(), f"sv-{job_id}.wav")
        out_base = os.path.join(tempfile.gettempdir(), f"sv-out-{job_id}")
        try:
            tset(job, status="estraggo l'audio", progress=0.0)
            extract_audio(video, wav)

            model = ensure_whisper_model(job)
            lang = config.get("tlang", "auto")
            tset(job, status="trascrivo", progress=0.0)
            run_whisper(wav, model, lang, out_base, job)
            with open(out_base + ".json") as f:
                full = json.load(f)
            words = tokens_to_words(full)
            if not words:
                raise RuntimeError("nessun parlato riconosciuto nel file")
            lang_out = (full.get("result") or {}).get("language") or lang

            diar_error = None
            try:
                ensure_python_deps(job)
                ensure_diar_models(job)
                tset(job, status="riconosco gli speaker — sui video lunghi "
                                 "servono alcuni minuti", progress=None)
                spk = config.get("tspeakers", "auto")
                n = int(spk) if spk != "auto" else -1
                assign_speakers(words, diarize_wav(
                    wav, n, on_progress=lambda p: tset(job, progress=p)))
            except Exception as e:            # senza speaker il transcript resta utile
                diar_error = str(e)

            txt, n_speakers = render_transcript_txt(
                words, os.path.basename(video), lang_out)
            stem = os.path.splitext(video)[0]
            with open(stem + ".transcript.txt", "w") as f:
                f.write(txt)
            with open(stem + ".transcript.json", "w") as f:
                json.dump({"text": " ".join(w["text"] for w in words),
                           "words": words, "language": lang_out,
                           "source": os.path.basename(video)}, f)

            note = f"{n_speakers} speaker" if n_speakers else None
            if diar_error:
                note = f"senza speaker ({diar_error[:120]})"
            with lock:
                extra_open.add(stem + ".transcript.txt")
            tset(job, status="fatto", progress=100.0,
                 txt=stem + ".transcript.txt", note=note)
        except Exception as e:
            tset(job, status="errore", error=str(e))
        finally:
            for p in (wav, out_base + ".json"):
                try:
                    os.remove(p)
                except OSError:
                    pass


# File fuori dalla cartella download che l'app stessa ha prodotto (transcript
# di video locali): /api/open può aprirli anche se non stanno in DEST.
extra_open = set()

pick_lock = threading.Lock()


def choose_local_file():
    """File picker nativo per scegliere un video/audio ovunque sul Mac.
    Con pywebview usa il dialog agganciato alla finestra; in modalità
    browser (server headless) ripiega su AppleScript."""
    try:
        import webview
        if webview.windows:
            r = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Video e audio (*.mp4;*.m4a;*.webm;*.mkv;*.mp3;*.mov)",))
            return r[0] if r else None
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose file with prompt '
             '"Scegli il video o l\'audio da trascrivere")'],
            capture_output=True, text=True)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except OSError:
        return None


def add_transcription(path, anywhere=False):
    """anywhere=True solo quando il path arriva dal file picker nativo
    (scelto dall'utente), mai da un path arbitrario mandato dalla pagina."""
    real = os.path.realpath(path)
    base = os.path.realpath(DEST)
    if not os.path.exists(real):
        return None, "file non trovato"
    if not anywhere and not real.startswith(base + os.sep):
        return None, "file non trovato"
    if not real.lower().endswith(TRANSCRIBE_EXTS):
        return None, "formato non trascrivibile"
    if FFMPEG == "ffmpeg" and not shutil.which("ffmpeg"):
        return None, "manca ffmpeg — installa con: brew install ffmpeg"
    if WHISPER == "whisper-cli" and not shutil.which("whisper-cli"):
        return None, "manca whisper — installa con: brew install whisper-cpp"
    with lock:
        active = any(t["path"] == real and t["status"] not in ("fatto", "errore")
                     for t in tjobs.values())
    if active:
        return None, "trascrizione già in corso per questo file"
    job_id = uuid.uuid4().hex[:8]
    with lock:
        tjobs[job_id] = {"id": job_id, "path": real,
                         "file": os.path.basename(real), "status": "in coda",
                         "progress": 0.0, "txt": None, "note": None, "error": None}
        tjobs_order.insert(0, job_id)
    threading.Thread(target=run_transcription, args=(job_id,), daemon=True).start()
    return job_id, None


# ---------------------------------------------------------------- download
def build_format_args(quality):
    if quality == "audio":
        return ["-x", "--audio-format", "m4a"]
    if quality == "max":                       # massima assoluta (anche VP9/AV1)
        return ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]
    if quality == "best":                      # massima H.264 (per DaVinci)
        return ["-S", "vcodec:h264,ext:mp4:m4a"]
    return ["-S", f"res:{quality},vcodec:h264,ext:mp4:m4a"]  # 1080 / 720


def run_gallery_dl(job, browser):
    """Foto e caroselli (Instagram & simili): yt-dlp non li gestisce, gallery-dl sì."""
    args = [GDL, "-D", DEST]
    if browser != "none":
        args += ["--cookies-from-browser", browser]
    args.append(job["url"])
    files = []
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            path = line.strip().lstrip("# ")
            if path.startswith("/"):
                files.append(path)
                with lock:
                    job["file"] = os.path.basename(path)
                    job["filepath"] = path
        proc.wait()
        return proc.returncode, files
    except Exception:
        return 1, files


# Errori di rete transitori (DNS che non risolve, timeout, reset): non è
# colpa del video, si riprova da soli invece di segnare subito "errore"
NET_ERRORS = ("failed to resolve", "nodename nor servname",
              "temporary failure", "timed out", "timeout",
              "connection reset", "network is unreachable",
              "getaddrinfo failed", "connection refused",
              "errno 8", "unable to connect")
# Errori che di solito si risolvono col login: si riprova coi cookie di Chrome
LOGIN_ERRORS = ("login", "logged-in", "logged in", "cookies",
                "authentication", "rate-limit", "not available")

ERRLOG = os.path.join(SUPPORT, "errors.log")


def log_error(url, browser, tail):
    """Diario degli errori di download (ultime righe di output yt-dlp):
    quando "si blocca di nuovo" qui c'è la prova di cosa è successo davvero."""
    try:
        if os.path.exists(ERRLOG) and os.path.getsize(ERRLOG) > 512 * 1024:
            os.replace(ERRLOG, ERRLOG + ".old")
        with open(ERRLOG, "a") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"browser={browser} {url}\n")
            f.write("\n".join(tail) + "\n")
    except OSError:
        pass


def run_job(job_id):
    with slots:
        with lock:
            job = jobs[job_id]
            job["status"] = "in corso"
        browser = job.get("browser", "none")
        net_retries = 0
        login_retries = 0
        # Il minutaggio entra nel nome file: tagli diversi dello stesso video
        # non collidono col file intero (yt-dlp salterebbe il download)
        outtmpl = "%(title).80s [%(id)s]"
        if job.get("section"):
            tag = job["section"].lstrip("*").replace(":", ".").replace("/", "-")
            outtmpl += f" [taglio {tag}]"
        while True:
            args = [YTDLP, "--newline", "--no-playlist",
                    "-N", str(config["fragments"]),
                    "-o", os.path.join(DEST, outtmpl + ".%(ext)s")]
            if browser != "none":
                args += ["--cookies-from-browser", browser]
            if job.get("section"):
                args += ["--download-sections", job["section"],
                         "--force-keyframes-at-cuts"]
            args += build_format_args(job["quality"])
            args.append(job["url"])

            tail = []
            try:
                proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        tail.append(line)
                        tail = tail[-6:]
                    with lock:
                        m = PROGRESS_RE.search(line)
                        if m:
                            job["progress"] = float(m.group(1))
                        m = SPEED_RE.search(line)
                        if m:
                            job["speed"] = m.group(1).strip()
                        m = ETA_RE.search(line)
                        if m:
                            job["eta"] = m.group(1)
                        m = SIZE_RE.search(line)
                        if m:
                            job["size"] = m.group(1).strip()
                        m = DEST_RE.search(line)
                        if m:
                            job["filepath"] = m.group(1)
                            job["file"] = os.path.basename(m.group(1))
                        m = ALREADY_RE.search(line)
                        if m:
                            job["filepath"] = m.group(1)
                            job["file"] = os.path.basename(m.group(1))
                            job["note"] = "file già presente — non riscaricato"
                proc.wait()
                if proc.returncode == 0:
                    with lock:
                        job["status"] = "fatto"
                        job["progress"] = 100.0
                        job["speed"] = job["eta"] = None
                    finish_job(job)
                    return
                error = tail[-1] if tail else f"exit {proc.returncode}"
            except FileNotFoundError:
                error = "yt-dlp non trovato — installa con: brew install yt-dlp ffmpeg"
            except Exception as e:
                error = str(e)

            low = error.lower()
            log_error(job["url"], browser, tail or [error])

            # Rete assente o instabile: aspetta e riprova (fino a 3 volte)
            if any(s in low for s in NET_ERRORS) and net_retries < 3:
                net_retries += 1
                with lock:
                    job["status"] = f"problema di rete — riprovo ({net_retries}/3)"
                    job["progress"] = 0.0
                time.sleep(4 * net_retries)
                continue

            # Post fotografici / caroselli: yt-dlp non trova video -> gallery-dl
            if "no video formats found" in low or "there is no video" in low:
                with lock:
                    job["status"] = "foto: uso gallery-dl"
                rc, files = run_gallery_dl(job, browser)
                with lock:
                    if rc == 0 and files:
                        job["status"] = "fatto"
                        job["progress"] = 100.0
                        if len(files) > 1:
                            job["file"] = f"{len(files)} file — ultimo: {os.path.basename(files[-1])}"
                    else:
                        job["status"] = "errore"
                        job["error"] = "gallery-dl non ha scaricato nulla"
                finish_job(job)
                return

            # TikTok, Instagram & co.: se l'errore parla di login/cookie,
            # riprova coi cookie di Chrome
            if browser == "none" and any(s in low for s in LOGIN_ERRORS):
                browser = "chrome"
                with lock:
                    job["browser"] = browser
                    job["status"] = "riprovo con login"
                continue

            # Il muro "requiring login" di TikTok è servito a campione, anche
            # con cookie validi (verificato: stessa URL, stessi cookie, a volte
            # passa e a volte no). Riprovare distanziati di solito sblocca.
            if "login" in low and login_retries < 3:
                login_retries += 1
                wait = (20, 45, 90)[login_retries - 1]
                with lock:
                    job["status"] = (f"il sito blocca a campione — riprovo "
                                     f"tra {wait}s ({login_retries}/3)")
                    job["progress"] = 0.0
                time.sleep(wait)
                continue

            # Muro ancora su dopo i retry ravvicinati: il blocco di TikTok
            # dura minuti. Libera lo slot e riprova da solo tra 5/10 minuti,
            # senza tenere occupata la coda.
            late = job.get("late_retries", 0)
            if "login" in low and late < 2:
                mins = 5 * (late + 1)
                with lock:
                    job["late_retries"] = late + 1
                    job["status"] = (f"sito bloccato — nuovo tentativo "
                                     f"automatico tra {mins} min")
                    job["progress"] = 0.0
                t = threading.Timer(mins * 60, run_job, args=(job_id,))
                t.daemon = True
                t.start()
                return

            if any(s in low for s in NET_ERRORS):
                error = "problema di rete (controlla la connessione) — " + error
            elif "login" in low and login_retries:
                error = ("il sito ha rifiutato tutti i tentativi (blocco a "
                         "campione) — aspetta qualche minuto e premi "
                         "Ri-scarica · " + error)
            with lock:
                job["status"] = "errore"
                job["error"] = error
            finish_job(job)
            return


def add_jobs(urls, quality, browser, section):
    added = 0
    for url in urls:
        if not url.startswith("http"):
            continue
        job_browser = browser
        if job_browser == "none" and "instagram.com" in url:
            job_browser = "chrome"     # Instagram richiede quasi sempre il login
        job_id = uuid.uuid4().hex[:8]
        with lock:
            jobs[job_id] = {"id": job_id, "url": url, "quality": quality,
                            "browser": job_browser, "section": section,
                            "status": "in coda", "progress": 0.0,
                            "speed": None, "eta": None, "size": None,
                            "file": None, "filepath": None, "error": None,
                            "note": None}
            jobs_order.insert(0, job_id)
        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
        added += 1
    return added


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json", code=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _authed(self):
        if self.headers.get("X-Token") == TOKEN:
            return True
        self._send({"error": "non autorizzato"}, code=403)
        return False

    def do_GET(self):
        if self.path == "/":
            self._send(HTML.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        elif self.path == "/ping":
            self._send("ok", "text/plain")
        elif not self._authed():
            return
        elif self.path == "/api/jobs":
            # Copia sotto lock, invio fuori: un client lento non deve
            # bloccare i thread di download
            with lock:
                data = {"done": done_count,
                        "jobs": [dict(jobs[j]) for j in jobs_order],
                        "tjobs": [dict(tjobs[j]) for j in tjobs_order]}
            self._send(data)
        elif self.path == "/api/history":
            self._send(load_history())
        elif self.path == "/api/settings":
            self._send(config)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._authed():
            return
        if self.path == "/api/add":
            d = self._body()
            urls = [u.strip() for u in d.get("urls", []) if u.strip()]
            section = ""
            if d.get("start") or d.get("end"):
                section = f"*{d.get('start') or '0:00'}-{d.get('end') or 'inf'}"
            added = add_jobs(urls, d.get("quality", "1080"),
                             d.get("browser", "none"), section)
            self._send({"added": added})
        elif self.path == "/api/open":
            d = self._body()
            # realpath: niente "../" per uscire dalla cartella dei download
            path = os.path.realpath(d.get("path", ""))
            base = os.path.realpath(DEST)
            with lock:
                allowed = path.startswith(base + os.sep) or path in extra_open
            if allowed and os.path.exists(path):
                reveal = d.get("reveal")
                subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
                self._send({"ok": True})
            else:
                self._send({"ok": False, "error": "file non trovato"}, code=404)
        elif self.path == "/api/open_folder":
            os.makedirs(DEST, exist_ok=True)
            subprocess.Popen(["open", DEST])
            self._send({"ok": True})
        elif self.path == "/api/transcribe":
            d = self._body()
            job_id, err = add_transcription(str(d.get("path", "")))
            if err:
                self._send({"ok": False, "error": err}, code=400)
            else:
                self._send({"ok": True, "id": job_id})
        elif self.path == "/api/transcribe_pick":
            # Il path esce dal file picker nativo, non dalla pagina: può
            # essere ovunque sul Mac. Un dialog alla volta.
            if not pick_lock.acquire(blocking=False):
                self._send({"ok": False, "error": "selettore file già aperto"},
                           code=409)
                return
            try:
                path = choose_local_file()
            finally:
                pick_lock.release()
            if not path:
                self._send({"ok": True, "canceled": True})
                return
            job_id, err = add_transcription(path, anywhere=True)
            if err:
                self._send({"ok": False, "error": err}, code=400)
            else:
                self._send({"ok": True, "id": job_id})
        elif self.path == "/api/preview":
            d = self._body()
            u = str(d.get("url", "")).strip()
            if not u.startswith("http"):
                self._send({"ok": False, "error": "url non valido"}, code=400)
                return
            src = resolve_preview(u)
            self._send({"ok": bool(src), "src": src})
        elif self.path == "/api/settings":
            d = self._body()
            dest = os.path.expanduser(str(d.get("dest") or config["dest"]).strip()
                                      or config["dest"])
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError as e:
                self._send({"ok": False, "error": f"cartella non valida: {e}"},
                           code=400)
                return
            config["dest"] = dest
            if d.get("quality") in {"1080", "720", "best", "max", "audio"}:
                config["quality"] = d["quality"]
            if d.get("browser") in {"none", "chrome", "safari", "firefox"}:
                config["browser"] = d["browser"]
            if d.get("tlang") in {"auto", "it", "en"}:
                config["tlang"] = d["tlang"]
            if d.get("tspeakers") in {"auto", "2", "3", "4"}:
                config["tspeakers"] = d["tspeakers"]
            try:
                config["concurrent"] = min(6, max(1, int(d.get("concurrent",
                                           config["concurrent"]))))
                config["fragments"] = min(8, max(1, int(d.get("fragments",
                                          config["fragments"]))))
            except (TypeError, ValueError):
                pass
            save_config()
            apply_config()
            self._send({"ok": True, **config})
        elif self.path == "/api/quit":
            self._send({"ok": True})
            threading.Timer(0.3, lambda: os._exit(0)).start()
        else:
            self.send_error(404)


# ---------------------------------------------------------------- ui
HTML = r"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scarica Video</title>
<style>
  :root { color-scheme: light dark;
    --bg:#f5f5f7; --card:#ffffff; --text:#1d1d1f; --muted:#86868b;
    --accent:#e11d48; --accent2:#ff6363; --ok:#34c759; --err:#ff3b30;
    --bar:#e8e8ed; --line:#e5e5ea; }
  @media (prefers-color-scheme: dark) { :root {
    --bg:#161617; --card:#1f1f21; --text:#f5f5f7; --muted:#98989d;
    --bar:#3a3a3c; --line:#2c2c2e; } }
  * { box-sizing:border-box; margin:0 }
  body { font:15px/1.5 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;
    background:var(--bg); color:var(--text); }
  .wrap { max-width:720px; margin:0 auto; padding:26px 20px 40px }
  header { display:flex; align-items:center; gap:12px; margin-bottom:18px }
  .logo { width:38px; height:38px; border-radius:10px;
    background:linear-gradient(160deg,var(--accent2),var(--accent));
    display:flex; align-items:center; justify-content:center; flex:0 0 auto }
  .logo svg { width:22px; height:22px; fill:#fff }
  h1 { font-size:20px; letter-spacing:-.02em }
  .sub { color:var(--muted); font-size:12px }
  .spacer { flex:1 }
  .ghost { background:none; border:1px solid var(--line); color:var(--muted);
    padding:6px 12px; border-radius:9px; font:inherit; font-size:12px; cursor:pointer }
  .ghost:hover { color:var(--text) }
  .panel { background:var(--card); border:1px solid var(--line);
    border-radius:16px; padding:16px; }
  textarea { width:100%; height:96px; padding:12px; border-radius:11px; resize:vertical;
    border:1px solid var(--line); background:var(--bg); color:var(--text); font:inherit }
  .controls { display:flex; gap:9px; margin-top:12px; align-items:center; flex-wrap:wrap }
  select, .seg input { padding:9px 11px; border-radius:10px; border:1px solid var(--line);
    background:var(--bg); color:var(--text); font:inherit; font-size:13px }
  .grow { flex:1 }
  button.go { padding:9px 22px; border:0; border-radius:10px;
    background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer }
  button.go:active { opacity:.75 }
  .interval { display:none; gap:9px; margin-top:11px; align-items:center; flex-wrap:wrap }
  .interval.on { display:flex }
  .interval label { font-size:12px; color:var(--muted) }
  .interval input { width:92px; text-align:center }
  .toggle { display:flex; align-items:center; gap:7px; font-size:13px; color:var(--muted);
    cursor:pointer; user-select:none }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted);
    margin:26px 4px 10px }
  .job, .hrow { background:var(--card); border:1px solid var(--line);
    border-radius:13px; padding:12px 14px; margin-bottom:9px }
  .name { font-weight:600; font-size:14px; word-break:break-word }
  .meta { font-size:12px; color:var(--muted); margin-top:2px; word-break:break-all }
  .badge { font-size:11px; font-weight:700; float:right; color:var(--muted); letter-spacing:.02em }
  .fatto .badge { color:var(--ok) } .errore .badge { color:var(--err) }
  .track { height:6px; background:var(--bar); border-radius:3px; margin-top:9px; overflow:hidden }
  .fill { height:100%; width:0; border-radius:3px; transition:width .4s;
    background:linear-gradient(90deg,var(--accent2),var(--accent)) }
  .fatto .fill { background:var(--ok) } .errore .fill { background:var(--err) }
  .hrow { display:flex; align-items:center; gap:10px }
  .hrow .info { flex:1; min-width:0 }
  .hrow .name { font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
  .acts { display:flex; gap:6px; flex:0 0 auto }
  .chip { border:1px solid var(--line); background:var(--bg); color:var(--text);
    border-radius:8px; padding:5px 9px; font-size:12px; cursor:pointer; white-space:nowrap }
  .chip:hover { border-color:var(--accent); color:var(--accent) }
  .empty { color:var(--muted); font-size:13px; padding:8px 4px }
  .dot { font-size:11px; color:var(--muted) }
  kbd { font:600 10px/1 -apple-system,system-ui,sans-serif; border:1px solid var(--line);
    border-bottom-width:2px; border-radius:4px; padding:2px 5px; background:var(--bg);
    color:var(--muted) }
  .pv { display:none; margin-top:12px }
  .pv.on { display:block }
  .pvframe { position:relative; width:100%; aspect-ratio:16/9; background:#000;
    border-radius:11px; overflow:hidden }
  .pvframe video { position:absolute; inset:0; width:100%; height:100%; border:0 }
  .pvmsg { position:absolute; inset:0; display:flex; align-items:center;
    justify-content:center; color:#fff; font-size:13px; text-align:center;
    padding:0 20px; background:rgba(0,0,0,.6) }
  .pvbar { display:flex; gap:9px; align-items:center; margin-top:9px; flex-wrap:wrap }
  .overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.45);
    z-index:50; align-items:center; justify-content:center }
  .overlay.on { display:flex }
  .sheet { background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:20px; width:min(460px,92vw) }
  .sheet h3 { font-size:16px; margin-bottom:14px }
  .field { margin-bottom:11px }
  .field label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px }
  .field input, .field select { width:100%; padding:9px 11px; border-radius:10px;
    border:1px solid var(--line); background:var(--bg); color:var(--text);
    font:inherit; font-size:13px }
  .cols { display:flex; gap:10px }
  .cols .field { flex:1 }
  .sheetacts { display:flex; gap:9px; justify-content:flex-end; margin-top:14px }
</style></head><body><div class="wrap">
<header>
  <div class="logo"><svg viewBox="0 0 24 24"><path d="M12 3v10.2l3.6-3.6L17 11l-5 5-5-5 1.4-1.4L12 13.2V3h0zM5 19h14v2H5z"/></svg></div>
  <div><h1>Scarica Video</h1><div class="sub">YouTube · TikTok · Instagram e centinaia di siti</div></div>
  <div class="spacer"></div>
  <button class="ghost" onclick="transcribeLocal()">Trascrivi file…</button>
  <button class="ghost" onclick="openFolder()">Apri cartella</button>
  <button class="ghost" onclick="openSettings()">Impostazioni</button>
  <button class="ghost" onclick="quitApp()">Esci</button>
</header>

<div class="panel">
  <textarea id="urls" oninput="checkPreview()" onpaste="setTimeout(normalizeUrls,0)" placeholder="Incolla uno o più link (uno per riga)&#10;https://www.youtube.com/watch?v=…&#10;https://www.tiktok.com/@utente/video/…"></textarea>
  <div class="controls">
    <select id="quality" class="grow">
      <option value="1080" selected>1080p — H.264 (per DaVinci)</option>
      <option value="720">720p — H.264</option>
      <option value="best">Massima qualità H.264</option>
      <option value="max">Massima assoluta (VP9/AV1)</option>
      <option value="audio">Solo audio (m4a)</option>
    </select>
    <select id="browser" title="Cookie dal browser: serve per i siti che chiedono il login">
      <option value="none" selected>Senza login</option>
      <option value="chrome">Login da Chrome</option>
      <option value="safari">Login da Safari</option>
      <option value="firefox">Login da Firefox</option>
    </select>
    <button class="go" onclick="add()">Scarica</button>
  </div>
  <div style="margin-top:11px">
    <label class="toggle"><input type="checkbox" id="useInterval" onchange="document.getElementById('iv').classList.toggle('on',this.checked)"> Solo un intervallo</label>
  </div>
  <div class="interval" id="iv">
    <label>DA</label><input id="start" placeholder="0:00">
    <label>A</label><input id="end" placeholder="1:30">
    <span class="dot">formato mm:ss · taglio preciso ai keyframe</span>
  </div>
  <div class="pv" id="pv">
    <div class="pvframe">
      <video id="yt" controls playsinline preload="metadata"></video>
      <div class="pvmsg" id="pvMsg">Carico la preview…</div>
    </div>
    <div class="pvbar">
      <button class="chip" onclick="setIn()"><kbd>I</kbd> IN <span id="inV">—</span></button>
      <button class="chip" onclick="setOut()"><kbd>O</kbd> OUT <span id="outV">—</span></button>
      <span class="dot">fermati sul punto giusto e premi I (inizio) e O (fine), come in DaVinci</span>
    </div>
  </div>
</div>

<div id="activeWrap" style="display:none"><h2>In download</h2><div id="active"></div></div>
<div id="twrap" style="display:none"><h2>Trascrizioni</h2><div id="tjobs"></div></div>
<h2>Cronologia</h2><div id="history"><div class="empty">Ancora niente.</div></div>

<div class="overlay" id="ovl" onclick="if(event.target===this)closeSettings()">
  <div class="sheet">
    <h3>Impostazioni</h3>
    <div class="field"><label>Cartella download</label><input id="sDest" spellcheck="false"></div>
    <div class="field"><label>Qualità predefinita</label>
      <select id="sQuality">
        <option value="1080">1080p — H.264 (per DaVinci)</option>
        <option value="720">720p — H.264</option>
        <option value="best">Massima qualità H.264</option>
        <option value="max">Massima assoluta (VP9/AV1)</option>
        <option value="audio">Solo audio (m4a)</option>
      </select></div>
    <div class="field"><label>Login predefinito (cookie dal browser)</label>
      <select id="sBrowser">
        <option value="none">Senza login</option>
        <option value="chrome">Chrome</option>
        <option value="safari">Safari</option>
        <option value="firefox">Firefox</option>
      </select></div>
    <div class="cols">
      <div class="field"><label>Download simultanei (1–6)</label><input id="sConc" type="number" min="1" max="6"></div>
      <div class="field"><label>Connessioni per video (1–8)</label><input id="sFrag" type="number" min="1" max="8"></div>
    </div>
    <div class="cols">
      <div class="field"><label>Trascrizione: lingua</label>
        <select id="sLang">
          <option value="auto">Rileva da sola</option>
          <option value="it">Italiano</option>
          <option value="en">Inglese</option>
        </select></div>
      <div class="field"><label>Trascrizione: speaker</label>
        <select id="sSpk">
          <option value="auto">Rileva da sola</option>
          <option value="2">2</option>
          <option value="3">3</option>
          <option value="4">4</option>
        </select></div>
    </div>
    <div class="sheetacts">
      <button class="ghost" onclick="closeSettings()">Annulla</button>
      <button class="go" onclick="saveSettings()">Salva</button>
    </div>
  </div>
</div>

<script>
const TOKEN = '__TOKEN__';
const $ = s => document.querySelector(s);
async function api(path, body){
  const opt = body===undefined ? {headers:{'X-Token':TOKEN}}
    : {method:'POST', headers:{'X-Token':TOKEN}, body:JSON.stringify(body)};
  return (await fetch(path, opt)).json();
}

function esc(s){ return (''+(s??'')).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function attr(v){ return esc(JSON.stringify(v)); }
function cls(s){ return s==='fatto'?'fatto':s==='errore'?'errore':'attivo'; }

// ------------------------------------------------------ azioni principali
async function add(){
  normalizeUrls();
  const urls = $('#urls').value.split('\n').filter(u=>u.trim());
  if(!urls.length) return;
  const body = { urls, quality:$('#quality').value, browser:$('#browser').value };
  if($('#useInterval').checked){ body.start=$('#start').value.trim(); body.end=$('#end').value.trim(); }
  await api('/api/add', body);
  // Con la preview attiva l'URL resta: comodo per scaricare più segmenti
  $('#urls').value = pvId ? $('#urls').value.split('\n')[0] : '';
  checkPreview(); refresh();
}
function openFolder(){ api('/api/open_folder',{}); }
function openFile(p,reveal){ api('/api/open',{path:p,reveal}); }
function redownload(url){ $('#urls').value = url; checkPreview(); normalizeUrls(); window.scrollTo({top:0,behavior:'smooth'}); $('#urls').focus(); }
async function quitApp(){ await api('/api/quit',{}); document.body.innerHTML='<div class="wrap"><p class="empty">Chiuso. Puoi chiudere questa finestra.</p></div>'; }

// ------------------------------------------------- preview YouTube: IN/OUT
// Niente iframe embedded (in WKWebView mostra solo "Watch on YouTube"):
// yt-dlp risolve l'URL diretto dello stream e lo riproduce un <video> nativo
let pvId = null, pvReq = 0;
function ytIdOf(u){
  const m = (u||'').match(/(?:youtube(?:-nocookie)?\.com\/(?:watch\?[^\s]*v=|shorts\/|live\/|embed\/|v\/)|youtu\.be\/)([\w-]{11})/);
  return m ? m[1] : null;
}
// Qualunque formato di link YouTube (watch, youtu.be, Shorts, live, embed,
// music/m., con playlist o secondaggio) -> URL canonico. Il ?t= diventa il
// punto di partenza nel campo DA, usato solo se attivi "Solo un intervallo".
function parseYt(u){
  const id = ytIdOf(u);
  if(!id) return null;
  let secs = 0;
  const t = (u.match(/[?&#](?:t|start)=(\d+(?:[hms]\d*)*s?)/i)||[])[1];
  if(t){
    if(/^\d+s?$/i.test(t)) secs = parseInt(t,10);
    else{
      const p = t.match(/^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$/i);
      if(p) secs = (+p[1]||0)*3600 + (+p[2]||0)*60 + (+p[3]||0);
    }
  }
  return { url:'https://www.youtube.com/watch?v='+id, start:secs };
}
function normalizeUrls(){
  const box = $('#urls');
  let changed = false;
  const out = box.value.split('\n').map(line=>{
    const y = parseYt(line.trim());
    if(!y) return line;
    if(y.start > 0 && !$('#start').value.trim()){
      $('#start').value = fmtT(y.start);
      $('#inV').textContent = fmtT(y.start);
    }
    if(y.url === line.trim()) return line;
    changed = true;
    return y.url;
  });
  if(changed){ box.value = out.join('\n'); checkPreview(); }
}
function pvMsg(t){ const m = $('#pvMsg'); m.textContent = t; m.style.display = t ? 'flex' : 'none'; }
function checkPreview(){
  const first = ($('#urls').value.split('\n')[0]||'').trim();
  const id = ytIdOf(first);
  if(id === pvId) return;
  pvId = id;
  const v = $('#yt');
  v.pause(); v.removeAttribute('src'); v.load();
  $('#inV').textContent = $('#outV').textContent = '—';
  if(!id){ $('#pv').classList.remove('on'); return; }
  $('#pv').classList.add('on');
  pvMsg('Carico la preview…');
  const my = ++pvReq;
  api('/api/preview', {url: first}).then(r=>{
    if(my !== pvReq) return;               // nel frattempo l'URL è cambiato
    if(r.ok && r.src){
      v.src = r.src; pvMsg('');
      // Link incollato con secondaggio: la preview parte dal punto giusto
      const st = $('#start').value.trim();
      if(/^\d+(:\d+){0,2}$/.test(st)){
        const s = st.split(':').reduce((a,x)=>a*60 + +x, 0);
        if(s) v.addEventListener('loadedmetadata', ()=>{ v.currentTime = s; }, {once:true});
      }
    }
    else pvMsg('Preview non disponibile per questo video — imposta DA e A a mano');
  }).catch(()=>{ if(my === pvReq) pvMsg('Preview non disponibile — imposta DA e A a mano'); });
}
function fmtT(t){
  t = Math.max(0, Math.round(t));
  const p = n => String(n).padStart(2,'0');
  const h = (t/3600)|0, m = ((t%3600)/60)|0, s = t%60;
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}
function enableInterval(){ $('#useInterval').checked = true; $('#iv').classList.add('on'); }
function setIn(){ if(!pvId) return; const v = fmtT(Math.floor($('#yt').currentTime||0)); $('#start').value = v; $('#inV').textContent = v; enableInterval(); }
function setOut(){ if(!pvId) return; const v = fmtT(Math.ceil($('#yt').currentTime||0)); $('#end').value = v; $('#outV').textContent = v; enableInterval(); }
document.addEventListener('keydown', e=>{
  if(e.key==='Escape'){ closeSettings(); return; }
  if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || !pvId) return;
  if(e.key==='i'||e.key==='I'){ e.preventDefault(); setIn(); }
  if(e.key==='o'||e.key==='O'){ e.preventDefault(); setOut(); }
});

// ------------------------------------------------------ trascrizione
function canTranscribe(name){ return /\.(mp4|m4a|webm|mkv|mp3|mov)$/i.test(name||''); }
async function transcribe(p){
  const r = await api('/api/transcribe', {path:p});
  if(!r.ok) alert(r.error||'Impossibile trascrivere');
  refresh();
}
// Trascrizione di un file qualunque sul Mac: il server apre il file picker
// nativo e mette in coda il file scelto
let picking = false;
async function transcribeLocal(){
  if(picking) return;
  picking = true;
  try{
    const r = await api('/api/transcribe_pick', {});
    if(!r.ok) alert(r.error||'Impossibile trascrivere');
  }catch(e){ alert('Impossibile aprire il selettore file'); }
  picking = false;
  refresh();
}
// ----------------------------------------------------------- impostazioni
function openSettings(){
  api('/api/settings').then(s=>{
    $('#sDest').value = s.dest; $('#sQuality').value = s.quality; $('#sBrowser').value = s.browser;
    $('#sConc').value = s.concurrent; $('#sFrag').value = s.fragments;
    $('#sLang').value = s.tlang||'auto'; $('#sSpk').value = s.tspeakers||'auto';
    $('#ovl').classList.add('on');
  });
}
function closeSettings(){ $('#ovl').classList.remove('on'); }
async function saveSettings(){
  const r = await api('/api/settings', { dest:$('#sDest').value.trim(),
    quality:$('#sQuality').value, browser:$('#sBrowser').value,
    concurrent:$('#sConc').value, fragments:$('#sFrag').value,
    tlang:$('#sLang').value, tspeakers:$('#sSpk').value });
  if(!r.ok){ alert(r.error||'Impossibile salvare'); return; }
  applyDefaults(r); closeSettings();
}
function applyDefaults(s){
  $('#quality').value = s.quality; $('#browser').value = s.browser;
}

// ----------------------------------------------------------------- polling
let lastDone = null;

async function loadHistory(){
  const hist = await api('/api/history');
  $('#history').innerHTML = hist.length ? hist.map(h=>{
    const ok = h.status==='fatto';
    const open = ok && h.filepath ? `<button class="chip" onclick='openFile(${attr(h.filepath)},false)'>Apri</button>
        ${canTranscribe(h.filepath)?`<button class="chip" onclick='transcribe(${attr(h.filepath)})'>Trascrivi</button>`:''}
        <button class="chip" onclick='openFile(${attr(h.filepath)},true)'>Finder</button>` : '';
    return `<div class="hrow ${cls(h.status)}">
      <div class="info">
        <div class="name">${esc(h.title||h.url)}</div>
        <div class="meta">${esc(h.created)} · ${esc(h.quality||'')}${h.section?(' · '+esc(h.section)):''}${h.note?(' · '+esc(h.note)):''}${h.error?(' · '+esc(h.error)):''}</div>
      </div>
      <div class="acts">${open}
        <button class="chip" onclick='redownload(${attr(h.url)})'>Ri-scarica</button>
      </div></div>`;
  }).join('') : '<div class="empty">Ancora niente.</div>';
}

async function refresh(){
  const r = await api('/api/jobs');
  const active = r.jobs;
  $('#activeWrap').style.display = active.length ? 'block':'none';
  $('#active').innerHTML = active.map(j=>{
    const line = j.status==='in corso'
      ? `${j.progress.toFixed(0)}% ${j.speed?('· '+j.speed):''} ${j.eta?('· ETA '+j.eta):''} ${j.size?('· '+j.size):''}`
      : esc(j.status);
    return `<div class="job ${cls(j.status)}">
      <span class="badge">${esc(j.status.toUpperCase())}</span>
      <div class="name">${esc(j.file||j.url)}</div>
      <div class="meta">${line}</div>
      <div class="track"><div class="fill" style="width:${j.progress}%"></div></div></div>`;
  }).join('');

  const tj = r.tjobs||[];
  const tActive = tj.some(t=>t.status!=='fatto'&&t.status!=='errore');
  $('#twrap').style.display = tj.length ? 'block':'none';
  $('#tjobs').innerHTML = tj.map(t=>{
    const done = t.status==='fatto', err = t.status==='errore';
    const pct = t.progress==null ? 100 : t.progress;
    const elapsed = t.since ? ' · da '+fmtT(Date.now()/1000 - t.since) : '';
    const line = done ? (t.note||'transcript pronto')
      : err ? esc(t.error||'errore')
      : `${esc(t.status)}${t.progress!=null&&t.progress>0?(' · '+t.progress.toFixed(0)+'%'):''}${elapsed}`;
    const acts = done&&t.txt ? `<div class="acts" style="margin-top:8px">
        <button class="chip" onclick='openFile(${attr(t.txt)},false)'>Apri transcript</button>
        <button class="chip" onclick='openFile(${attr(t.txt)},true)'>Finder</button></div>` : '';
    return `<div class="job ${cls(t.status)}">
      <span class="badge">${esc(t.status.toUpperCase())}</span>
      <div class="name">${esc(t.file)}</div>
      <div class="meta">${line}</div>
      <div class="track"><div class="fill" style="width:${pct}%"></div></div>${acts}</div>`;
  }).join('');

  // Cronologia e cartella cambiano solo quando un job finisce
  if(r.done !== lastDone){
    lastDone = r.done;
    loadHistory();
  }

  clearTimeout(window._t);
  window._t = setTimeout(refresh, (active.length||tActive)?800:4000);
}
api('/api/settings').then(applyDefaults);
refresh();
</script></div></body></html>"""


def set_mac_identity():
    """Icona nel Dock/Cmd-Tab e nome dell'app, anche girando dentro un venv."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "applet.icns")
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Scarica Video"
        app = NSApplication.sharedApplication()
        if os.path.exists(icon):
            app.setApplicationIconImage_(NSImage.alloc().initByReferencingFile_(icon))
    except Exception:
        pass


def update_ytdlp_daily():
    """yt-dlp smette di funzionare quando i siti cambiano qualcosa: tenerlo
    aggiornato è la prima difesa. Al massimo una volta al giorno, in
    background, in silenzio; se fallisce (offline) riproverà al prossimo avvio."""
    py = os.path.join(SUPPORT, "venv", "bin", "python3")
    stamp = os.path.join(SUPPORT, "ytdlp-update.stamp")
    if not os.path.exists(py):
        return
    try:
        if time.time() - os.path.getmtime(stamp) < 86400:
            return
    except OSError:
        pass

    def worker():
        r = subprocess.run([py, "-m", "pip", "install", "--quiet",
                            "--disable-pip-version-check", "--upgrade",
                            "yt-dlp[default,curl-cffi]", "gallery-dl"],
                           capture_output=True)
        if r.returncode == 0:
            with open(stamp, "w") as f:
                f.write(str(time.time()))

    threading.Thread(target=worker, daemon=True).start()


def start_server():
    global server
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    init_db()
    load_config()
    os.makedirs(DEST, exist_ok=True)
    update_ytdlp_daily()
    url = f"http://127.0.0.1:{PORT}/"
    # Avvia il server; se la porta è occupata un'istanza è già attiva -> apri solo la finestra
    try:
        start_server()
    except OSError:
        pass
    try:
        import webview
        set_mac_identity()
        webview.create_window("Scarica Video", url, width=780, height=920,
                              min_size=(560, 640))
        webview.start()          # blocca finché la finestra resta aperta; chiusura = uscita
    except ImportError:
        # Nessun pywebview (uso da riga di comando): resta come server headless
        print(f"Scarica Video → {url}   (Ctrl+C per uscire)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
