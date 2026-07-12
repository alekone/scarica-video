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
YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
GDL = shutil.which("gallery-dl") or "/opt/homebrew/bin/gallery-dl"
MAX_CONCURRENT = 3

jobs = {}
jobs_order = []
lock = threading.Lock()
slots = threading.Semaphore(MAX_CONCURRENT)
db_lock = threading.Lock()

PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
SPEED_RE = re.compile(r"at\s+([\d.]+\s*[KMGT]?i?B/s|Unknown[^\s]*)")
ETA_RE = re.compile(r"ETA\s+([\d:]+)")
SIZE_RE = re.compile(r"of\s+~?\s*([\d.]+\s*[KMGT]?i?B)")
DEST_RE = re.compile(
    r'(?:Destination:|Merging formats into|has already been downloaded)\s*"?'
    r'((?:/[^"\n]+?)\.(?:mp4|m4a|webm|mkv|mp3|mov))"?'
)


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


def save_history(job):
    with db_lock, db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO history "
            "(id,url,title,filepath,quality,section,status,error) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job["id"], job["url"], job.get("file"), job.get("filepath"),
             job.get("quality"), job.get("section") or "", job["status"],
             job.get("error")),
        )


def load_history(limit=200):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM history ORDER BY created DESC LIMIT ?", (limit,)
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
        while True:
            args = [YTDLP, "--newline", "--no-playlist",
                    "-o", os.path.join(DEST, "%(title).80s [%(id)s].%(ext)s")]
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
                proc.wait()
                if proc.returncode == 0:
                    with lock:
                        job["status"] = "fatto"
                        job["progress"] = 100.0
                        job["speed"] = job["eta"] = None
                    save_history(job)
                    return
                error = tail[-1] if tail else f"exit {proc.returncode}"
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
                save_history(job)
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
            save_history(job)
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
                            "file": None, "filepath": None, "error": None}
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
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path == "/":
            self._send(HTML, "text/html; charset=utf-8")
        elif self.path == "/ping":
            self._send("ok", "text/plain")
        elif self.path == "/api/jobs":
            with lock:
                self._send([jobs[j] for j in jobs_order])
        elif self.path == "/api/history":
            self._send(load_history())
        else:
            self.send_error(404)

    def do_POST(self):
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
            path = d.get("path", "")
            if path.startswith(DEST) and os.path.exists(path):
                reveal = d.get("reveal")
                subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
                self._send({"ok": True})
            else:
                self._send({"ok": False, "error": "file non trovato"}, code=404)
        elif self.path == "/api/open_folder":
            os.makedirs(DEST, exist_ok=True)
            subprocess.Popen(["open", DEST])
            self._send({"ok": True})
        elif self.path == "/api/quit":
            self._send({"ok": True})
            threading.Thread(target=lambda: (server.shutdown()), daemon=True).start()
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
  .wrap { max-width:720px; margin:0 auto; padding:26px 20px 60px }
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
</style></head><body><div class="wrap">
<header>
  <div class="logo"><svg viewBox="0 0 24 24"><path d="M12 3v10.2l3.6-3.6L17 11l-5 5-5-5 1.4-1.4L12 13.2V3h0zM5 19h14v2H5z"/></svg></div>
  <div><h1>Scarica Video</h1><div class="sub">YouTube · TikTok · Instagram e centinaia di siti</div></div>
  <div class="spacer"></div>
  <button class="ghost" onclick="openFolder()">Apri cartella</button>
  <button class="ghost" onclick="quitApp()">Esci</button>
</header>

<div class="panel">
  <textarea id="urls" placeholder="Incolla uno o più link (uno per riga)&#10;https://www.youtube.com/watch?v=…&#10;https://www.tiktok.com/@utente/video/…"></textarea>
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
</div>

<div id="activeWrap" style="display:none"><h2>In download</h2><div id="active"></div></div>
<h2>Cronologia</h2><div id="history"><div class="empty">Ancora niente. I file finiscono in ~/Movies/Scarica Video.</div></div>

<script>
const $ = s => document.querySelector(s);
async function post(path, body){ return (await fetch(path,{method:'POST',body:JSON.stringify(body||{})})).json(); }

async function add(){
  const urls = $('#urls').value.split('\n').filter(u=>u.trim());
  if(!urls.length) return;
  const body = { urls, quality:$('#quality').value, browser:$('#browser').value };
  if($('#useInterval').checked){ body.start=$('#start').value.trim(); body.end=$('#end').value.trim(); }
  await post('/api/add', body);
  $('#urls').value=''; refresh();
}
function openFolder(){ post('/api/open_folder'); }
function openFile(p,reveal){ post('/api/open',{path:p,reveal}); }
function redownload(url){ $('#urls').value = url; window.scrollTo({top:0,behavior:'smooth'}); $('#urls').focus(); }
async function quitApp(){ await post('/api/quit'); document.body.innerHTML='<div class="wrap"><p class="empty">Chiuso. Puoi chiudere questa finestra.</p></div>'; }

function esc(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function refresh(){
  const jobs = await (await fetch('/api/jobs')).json();
  const active = jobs.filter(j=>j.status!=='fatto'&&j.status!=='errore');
  $('#activeWrap').style.display = active.length ? 'block':'none';
  $('#active').innerHTML = active.map(j=>{
    const line = j.status==='in corso'
      ? `${j.progress.toFixed(0)}% ${j.speed?('· '+j.speed):''} ${j.eta?('· ETA '+j.eta):''} ${j.size?('· '+j.size):''}`
      : esc(j.status);
    return `<div class="job ${j.status}">
      <span class="badge">${esc(j.status.toUpperCase())}</span>
      <div class="name">${esc(j.file||j.url)}</div>
      <div class="meta">${line}</div>
      <div class="track"><div class="fill" style="width:${j.progress}%"></div></div></div>`;
  }).join('');

  const hist = await (await fetch('/api/history')).json();
  $('#history').innerHTML = hist.length ? hist.map(h=>{
    const ok = h.status==='fatto';
    const open = ok && h.filepath ? `<button class="chip" onclick='openFile(${JSON.stringify(h.filepath)},false)'>Apri</button>
        <button class="chip" onclick='openFile(${JSON.stringify(h.filepath)},true)'>Finder</button>` : '';
    return `<div class="hrow ${h.status}">
      <div class="info">
        <div class="name">${esc(h.title||h.url)}</div>
        <div class="meta">${esc(h.created)} · ${esc(h.quality||'')}${h.section?(' · '+esc(h.section)):''}${h.error?(' · '+esc(h.error)):''}</div>
      </div>
      <div class="acts">${open}
        <button class="chip" onclick='redownload(${JSON.stringify(h.url)})'>Ri-scarica</button>
      </div></div>`;
  }).join('') : '<div class="empty">Ancora niente. I file finiscono in ~/Movies/Scarica Video.</div>';

  const busy = jobs.some(j=>j.status==='in coda'||j.status==='in corso'||j.status.startsWith('foto')||j.status.startsWith('riprovo'));
  clearTimeout(window._t); window._t = setTimeout(refresh, busy?800:4000);
}
refresh();
</script></div></body></html>"""


if __name__ == "__main__":
    init_db()
    os.makedirs(DEST, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Scarica Video → http://127.0.0.1:{PORT}   (Ctrl+C per uscire)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
