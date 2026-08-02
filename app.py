#!/usr/bin/env python3
"""
Scarica Video — app locale per scaricare video con yt-dlp.
YouTube, TikTok, Instagram e centinaia di altri siti. Solo Python stdlib.

- Coda con download in parallelo, progress bar, velocità ed ETA
- Intervallo di minutaggio (DA -> A) con taglio preciso ai keyframe
- Cronologia persistente (SQLite) con apri file / mostra nel Finder / ri-scarica
- Login via cookie del browser per i siti che lo richiedono (TikTok, Instagram)
- Fallback su gallery-dl per foto e caroselli
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8642
DEST = os.path.expanduser("~/Movies/Scarica Video")
SUPPORT = os.path.expanduser("~/Library/Application Support/Scarica Video")
DB_PATH = os.path.join(SUPPORT, "history.db")


def find_bin(name):
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
# Token per-avvio: le API rispondono solo alla nostra pagina, non ad altri
# siti aperti nel browser (CSRF su 127.0.0.1)
TOKEN = uuid.uuid4().hex

CONFIG_PATH = os.path.join(SUPPORT, "config.json")
CONFIG_DEFAULTS = {"dest": DEST, "quality": "1080", "browser": "none",
                   "concurrent": 3, "fragments": 4}
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


MEDIA_EXTS = (".mp4", ".m4a", ".webm", ".mkv", ".mp3", ".mov",
              ".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif")


def list_files(limit=80):
    try:
        entries = list(os.scandir(DEST))
    except OSError:
        return []
    out = []
    for e in entries:
        if e.name.startswith(".") or not e.name.lower().endswith(MEDIA_EXTS):
            continue
        try:
            if not e.is_file():
                continue
            st = e.stat()
        except OSError:
            continue
        out.append({"name": e.name, "path": e.path,
                    "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: -f["mtime"])
    return out[:limit]


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


def run_job(job_id):
    with slots:
        with lock:
            job = jobs[job_id]
            job["status"] = "in corso"
        browser = job.get("browser", "none")
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

            try:
                proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                tail = []
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

            # TikTok & co.: se serve il login, riprova coi cookie di Chrome
            if browser == "none" and "login" in low:
                browser = "chrome"
                with lock:
                    job["browser"] = browser
                    job["status"] = "riprovo con login"
                continue

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
                        "jobs": [dict(jobs[j]) for j in jobs_order]}
            self._send(data)
        elif self.path == "/api/history":
            self._send(load_history())
        elif self.path == "/api/files":
            self._send(list_files())
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
            if path.startswith(base + os.sep) and os.path.exists(path):
                reveal = d.get("reveal")
                subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
                self._send({"ok": True})
            else:
                self._send({"ok": False, "error": "file non trovato"}, code=404)
        elif self.path == "/api/open_folder":
            os.makedirs(DEST, exist_ok=True)
            subprocess.Popen(["open", DEST])
            self._send({"ok": True})
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
  .wrap { max-width:720px; margin:0 auto; padding:26px 20px 250px }
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
  .pvframe iframe { position:absolute; inset:0; width:100%; height:100%; border:0 }
  .pvbar { display:flex; gap:9px; align-items:center; margin-top:9px; flex-wrap:wrap }
  .fm { position:fixed; left:0; right:0; bottom:0; z-index:40; background:var(--card);
    border-top:1px solid var(--line) }
  .fmhead { display:flex; align-items:center; gap:9px; padding:9px 20px; cursor:pointer;
    font-size:13px; font-weight:600; max-width:720px; margin:0 auto }
  .fmbody { max-height:172px; overflow-y:auto; padding:0 20px 12px;
    max-width:720px; margin:0 auto }
  .fm.closed .fmbody { display:none }
  .frow { display:flex; align-items:center; gap:10px; background:var(--bg);
    border:1px solid var(--line); border-radius:10px; padding:8px 11px; margin-bottom:6px }
  .fname { flex:1; min-width:0; font-size:13px; font-weight:600; color:var(--text);
    text-decoration:none; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    cursor:grab }
  .fmeta { font-size:11px; color:var(--muted); white-space:nowrap }
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
  <button class="ghost" onclick="openFolder()">Apri cartella</button>
  <button class="ghost" onclick="openSettings()">Impostazioni</button>
  <button class="ghost" onclick="quitApp()">Esci</button>
</header>

<div class="panel">
  <textarea id="urls" oninput="checkPreview()" placeholder="Incolla uno o più link (uno per riga)&#10;https://www.youtube.com/watch?v=…&#10;https://www.tiktok.com/@utente/video/…"></textarea>
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
    <div class="pvframe"><iframe id="yt" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe></div>
    <div class="pvbar">
      <button class="chip" onclick="setIn()"><kbd>I</kbd> IN <span id="inV">—</span></button>
      <button class="chip" onclick="setOut()"><kbd>O</kbd> OUT <span id="outV">—</span></button>
      <span class="dot">pausa sul punto giusto, poi I / O · clicca fuori dal player per usare la tastiera</span>
    </div>
  </div>
</div>

<div id="activeWrap" style="display:none"><h2>In download</h2><div id="active"></div></div>
<h2>Cronologia</h2><div id="history"><div class="empty">Ancora niente.</div></div>

<div class="fm" id="fm">
  <div class="fmhead" onclick="toggleFm()">
    <span>📁</span><span id="fmTitle">Cartella download</span>
    <span class="dot" id="fmCount"></span>
    <span class="spacer"></span>
    <button class="ghost" onclick="event.stopPropagation();loadFiles()">Aggiorna</button>
  </div>
  <div class="fmbody" id="files"></div>
</div>

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
function redownload(url){ $('#urls').value = url; checkPreview(); window.scrollTo({top:0,behavior:'smooth'}); $('#urls').focus(); }
async function quitApp(){ await api('/api/quit',{}); document.body.innerHTML='<div class="wrap"><p class="empty">Chiuso. Puoi chiudere questa finestra.</p></div>'; }

// ------------------------------------------------- preview YouTube: IN/OUT
let pvId = null, ytTime = 0;
function ytIdOf(u){
  const m = (u||'').match(/(?:youtube\.com\/(?:watch\?[^\s]*v=|shorts\/|live\/|embed\/)|youtu\.be\/)([\w-]{11})/);
  return m ? m[1] : null;
}
function checkPreview(){
  const id = ytIdOf(($('#urls').value.split('\n')[0]||'').trim());
  if(id === pvId) return;
  pvId = id; ytTime = 0;
  if(id){
    $('#yt').src = `https://www.youtube.com/embed/${id}?enablejsapi=1&rel=0&origin=${encodeURIComponent(location.origin)}`;
    $('#pv').classList.add('on');
  } else {
    $('#yt').src = 'about:blank';
    $('#pv').classList.remove('on');
    $('#inV').textContent = $('#outV').textContent = '—';
  }
}
window.addEventListener('message', e=>{
  if(e.origin!=='https://www.youtube.com') return;
  let d; try{ d = JSON.parse(e.data); }catch(_){ return; }
  if(d.event==='infoDelivery' && d.info && typeof d.info.currentTime==='number') ytTime = d.info.currentTime;
});
$('#yt').addEventListener('load', ()=>{
  // Handshake col player: da qui in poi manda i suoi infoDelivery (currentTime)
  const hi = ()=>{ try{ $('#yt').contentWindow.postMessage(JSON.stringify({event:'listening', id:'sv', channel:'widget'}), '*'); }catch(_){} };
  hi(); setTimeout(hi, 800); setTimeout(hi, 2500);
});
function fmtT(t){
  t = Math.max(0, Math.round(t));
  const p = n => String(n).padStart(2,'0');
  const h = (t/3600)|0, m = ((t%3600)/60)|0, s = t%60;
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}
function enableInterval(){ $('#useInterval').checked = true; $('#iv').classList.add('on'); }
function setIn(){ if(!pvId) return; const v = fmtT(Math.floor(ytTime)); $('#start').value = v; $('#inV').textContent = v; enableInterval(); }
function setOut(){ if(!pvId) return; const v = fmtT(Math.ceil(ytTime)); $('#end').value = v; $('#outV').textContent = v; enableInterval(); }
document.addEventListener('keydown', e=>{
  if(e.key==='Escape'){ closeSettings(); return; }
  if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || !pvId) return;
  if(e.key==='i'||e.key==='I'){ e.preventDefault(); setIn(); }
  if(e.key==='o'||e.key==='O'){ e.preventDefault(); setOut(); }
});

// ------------------------------------------------------ cartella download
function fmtSize(b){ return b>=1e9 ? (b/1e9).toFixed(2)+' GB' : b>=1e6 ? (b/1e6).toFixed(1)+' MB' : Math.max(1,Math.round(b/1e3))+' KB'; }
function fmtDate(t){ const d = new Date(t*1000);
  return d.toLocaleDateString('it-IT',{day:'numeric',month:'short'})+' '+d.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'}); }
async function loadFiles(){
  const fs = await api('/api/files');
  $('#fmCount').textContent = fs.length ? fs.length+' file · trascinali in DaVinci' : '';
  $('#files').innerHTML = fs.length ? fs.map(f=>`
    <div class="frow">
      <a class="fname" draggable="true" href="file://${esc(encodeURI(f.path))}"
         ondragstart='dragFile(event,${attr(f.path)})' onclick="return false"
         ondblclick='openFile(${attr(f.path)},false)' title="${esc(f.name)} — doppio click per aprire">${esc(f.name)}</a>
      <span class="fmeta">${fmtSize(f.size)} · ${fmtDate(f.mtime)}</span>
      <button class="chip" onclick='openFile(${attr(f.path)},true)'>Finder</button>
    </div>`).join('') : '<div class="empty">Cartella vuota.</div>';
}
function dragFile(e,p){
  const u = 'file://'+encodeURI(p);
  e.dataTransfer.setData('text/uri-list', u);
  e.dataTransfer.setData('text/plain', p);
  e.dataTransfer.effectAllowed = 'copy';
}
function toggleFm(){ $('#fm').classList.toggle('closed'); }

// ----------------------------------------------------------- impostazioni
function openSettings(){
  api('/api/settings').then(s=>{
    $('#sDest').value = s.dest; $('#sQuality').value = s.quality; $('#sBrowser').value = s.browser;
    $('#sConc').value = s.concurrent; $('#sFrag').value = s.fragments;
    $('#ovl').classList.add('on');
  });
}
function closeSettings(){ $('#ovl').classList.remove('on'); }
async function saveSettings(){
  const r = await api('/api/settings', { dest:$('#sDest').value.trim(),
    quality:$('#sQuality').value, browser:$('#sBrowser').value,
    concurrent:$('#sConc').value, fragments:$('#sFrag').value });
  if(!r.ok){ alert(r.error||'Impossibile salvare'); return; }
  applyDefaults(r); closeSettings(); loadFiles();
}
function applyDefaults(s){
  $('#quality').value = s.quality; $('#browser').value = s.browser;
  $('#fmTitle').textContent = s.dest.replace(/^\/Users\/[^/]+/, '~');
}

// ----------------------------------------------------------------- polling
let lastDone = null;

async function loadHistory(){
  const hist = await api('/api/history');
  $('#history').innerHTML = hist.length ? hist.map(h=>{
    const ok = h.status==='fatto';
    const open = ok && h.filepath ? `<button class="chip" onclick='openFile(${attr(h.filepath)},false)'>Apri</button>
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

  // Cronologia e cartella cambiano solo quando un job finisce
  if(r.done !== lastDone){
    lastDone = r.done;
    loadHistory(); loadFiles();
  }

  clearTimeout(window._t); window._t = setTimeout(refresh, active.length?800:4000);
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


def start_server():
    global server
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    init_db()
    load_config()
    os.makedirs(DEST, exist_ok=True)
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
